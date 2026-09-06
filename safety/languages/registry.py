from pathlib import Path
from typing import Dict, List, Optional
from safety.languages.base import BaseLanguageDriver

_DRIVERS_BY_EXT: Dict[str, BaseLanguageDriver] = {}
_DRIVERS_BY_LANG: Dict[str, BaseLanguageDriver] = {}


def register_driver(driver: BaseLanguageDriver) -> None:
    _DRIVERS_BY_LANG[driver.language_id.lower()] = driver
    for ext in driver.supported_extensions:
        _DRIVERS_BY_EXT[ext.lower()] = driver


def get_driver_for_file(file_path: str) -> Optional[BaseLanguageDriver]:
    if not file_path:
        return None
    suffix = Path(file_path).suffix.lower()
    return _DRIVERS_BY_EXT.get(suffix)


def get_driver_for_language(lang: str) -> Optional[BaseLanguageDriver]:
    if not lang:
        return None
    return _DRIVERS_BY_LANG.get(lang.lower())


def get_all_registered_drivers() -> List[BaseLanguageDriver]:
    return list(_DRIVERS_BY_LANG.values())


def clear_registry() -> None:
    _DRIVERS_BY_EXT.clear()
    _DRIVERS_BY_LANG.clear()
