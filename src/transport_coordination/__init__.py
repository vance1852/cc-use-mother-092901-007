"""技能赛训协作基础服务的服务端基础包。"""

from .claims_service import ClaimsService
from .service import DomainService

__all__ = ["DomainService", "ClaimsService"]
