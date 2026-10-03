"""技能赛训协作基础服务的服务端基础包。"""

from .claims import ClaimsService
from .service import DomainService

__all__ = ["ClaimsService", "DomainService"]
