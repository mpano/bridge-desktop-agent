from dataclasses import dataclass


@dataclass(frozen=True)
class Preference:
    key: str
    value: str
