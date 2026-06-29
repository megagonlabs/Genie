from enum import StrEnum


class Scope(StrEnum):
    SELF = "self"
    SUBTREE = "subtree"
    ANCESTORS = "ancestors"
    LINKS = "links"
    GLOBAL = "global"
