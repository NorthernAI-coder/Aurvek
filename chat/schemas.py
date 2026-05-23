from typing import Optional

from pydantic import BaseModel


class BranchConversationRequest(BaseModel):
    message_id: int
    folder_id: Optional[int] = None


class NewConversationRequest(BaseModel):
    prompt_id: Optional[int] = None
    folder_id: Optional[int] = None
    llm_id: Optional[int] = None
    incognito: bool = False
