"""Data models for the Agent Gateway."""

from pydantic import BaseModel


# Job state constants
INV_STATE_QUEUED = "queued"
INV_STATE_RUNNING = "running"
INV_STATE_COMPLETED = "completed"
INV_STATE_FAILED = "failed"


class AgentsFilter(BaseModel):
    """Filter criteria for listing agents."""

    state: str = "running"  # running | stopped | all
