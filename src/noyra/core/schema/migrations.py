"""Ordered historical migrations, collected without changing SQL or versions."""

from __future__ import annotations

from .migrations_v002_v005 import MIGRATIONS_V002_V005
from .migrations_v006_v017 import MIGRATIONS_V006_V017
from .migrations_v018_v021 import MIGRATIONS_V018_V021
from .migrations_v022_v034 import MIGRATIONS_V022_V034
from .migrations_v035_v042 import MIGRATIONS_V035_V042
from .migrations_v043_v052 import MIGRATIONS_V043_V052
from .migrations_v053_v054 import MIGRATIONS_V053_V054
from .migrations_v055_v069 import MIGRATIONS_V055_V069
from .migrations_v070_v107 import MIGRATIONS_V070_V107

MIGRATIONS: dict[int, str] = {
    **MIGRATIONS_V002_V005,
    **MIGRATIONS_V006_V017,
    **MIGRATIONS_V018_V021,
    **MIGRATIONS_V022_V034,
    **MIGRATIONS_V035_V042,
    **MIGRATIONS_V043_V052,
    **MIGRATIONS_V053_V054,
    **MIGRATIONS_V055_V069,
    **MIGRATIONS_V070_V107,
}
