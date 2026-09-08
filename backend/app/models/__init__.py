"""SQLAlchemy models.

Imported for their side effect of registering with Base.metadata, which Alembic
autogenerate and the test fixtures both rely on.
"""

from app.models.base import Base
from app.models.fabric import Fabric, FabricMember, Link
from app.models.job import AuditEvent, Job
from app.models.policy import AppGroup, Policy, SdwanGroup, SlaProfile
from app.models.site import Site, Wan
from app.models.token import ApiToken
from app.models.user import User

__all__ = [
    "ApiToken",
    "AppGroup",
    "AuditEvent",
    "Base",
    "Fabric",
    "FabricMember",
    "Job",
    "Link",
    "Policy",
    "Site",
    "SdwanGroup",
    "SlaProfile",
    "User",
    "Wan",
]
