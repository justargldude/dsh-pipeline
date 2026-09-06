from safety.languages.base import BaseLanguageDriver, ASTViolationError, PurityViolationError, PurityReport
from safety.languages.registry import (
    register_driver,
    get_driver_for_file,
    get_driver_for_language,
    get_all_registered_drivers,
    clear_registry,
)
from safety.languages.csharp import CSharpDriver
from safety.languages.python import PythonDriver

# Register built-in drivers
register_driver(CSharpDriver())
register_driver(PythonDriver())

try:
    from safety.languages.javascript import JavascriptDriver
    register_driver(JavascriptDriver())
except Exception:
    pass

try:
    from safety.languages.cpp import CppDriver
    register_driver(CppDriver())
except Exception:
    pass

__all__ = [
    "BaseLanguageDriver",
    "ASTViolationError",
    "PurityViolationError",
    "PurityReport",
    "register_driver",
    "get_driver_for_file",
    "get_driver_for_language",
    "get_all_registered_drivers",
    "clear_registry",
    "CSharpDriver",
    "PythonDriver",
]
