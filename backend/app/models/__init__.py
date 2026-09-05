"""SQLAlchemy models.

Imported for their side effect of registering with Base.metadata, which Alembic
autogenerate and the test fixtures both rely on.
"""

from app.models.base import Base
from app.models.fabric import Fabric, FabricMember, Link
from app.models.job import AuditEvent, Job
from app.models.policy import AppGroup, Policy, SlaProfile
from app.models.site import Site, Wan
from app.models.user import User

__all__ = [
    "AppGroup",
    "AuditEvent",
    "Base",
    "Fabric",
    "FabricMember",
    "Job",
    "Link",
    "Policy",
    "Site",
    "SlaProfile",
    "User",
    "Wan",
]
