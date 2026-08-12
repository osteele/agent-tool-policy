"""Central registry for all Bash policy domains."""

from .development import POLICIES as DEVELOPMENT_POLICIES
from .models import Policy
from .remote_jobs import POLICIES as REMOTE_JOBS_POLICIES
from .research import POLICIES as RESEARCH_POLICIES
from .transfers import POLICIES as TRANSFER_POLICIES

POLICIES: tuple[Policy, ...] = (
    *RESEARCH_POLICIES,
    *DEVELOPMENT_POLICIES,
    *REMOTE_JOBS_POLICIES,
    *TRANSFER_POLICIES,
)
