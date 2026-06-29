from typing import TYPE_CHECKING, Any

KaligoBase: Any
if TYPE_CHECKING:
    from .bot import Kaligo

    KaligoBase = Kaligo
else:
    import abc

    KaligoBase = abc.ABC
