from typing import TYPE_CHECKING, Any

from .base import KaligoBase
from .database import AsyncClient, AsyncDatabase

if TYPE_CHECKING:
    from .bot import Kaligo


class DatabaseProvider(KaligoBase):
    db: AsyncDatabase

    def __init__(self: "Kaligo", **kwargs: Any) -> None:
        client = AsyncClient(self.config["bot"]["db_uri"], connect=False)
        self.db = client.get_database("KALIGO")

        # Propagate initialization to other mixins
        super().__init__(**kwargs)
