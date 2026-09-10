"""接口层：请求/响应模型（Pydantic）。"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities import ChapterStatus, ForeshadowStatus, OutlineLevel


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ================= Novel =================
class NovelCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    genre: str = ""
    summary: str = ""


class NovelUpdate(BaseModel):
    title: Optional[str] = None
    genre: Optional[str] = None
    summary: Optional[str] = None
    style_anchor: Optional[str] = None


class NovelRead(ORMBase):
    id: str
    title: str
    genre: str
    summary: str
    style_anchor: str
    created_at: str
    updated_at: str


# ================= Character =================
class CharacterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    role: str = ""
    personality: str = ""
    goal: str = ""
    appearance: str = ""
    catchphrase: str = ""
    pov: str = ""
    frequency: str = ""
    known_info: str = ""
    extras: dict = Field(default_factory=dict)


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    personality: Optional[str] = None
    goal: Optional[str] = None
    appearance: Optional[str] = None
    catchphrase: Optional[str] = None
    pov: Optional[str] = None
    frequency: Optional[str] = None
    known_info: Optional[str] = None
    extras: Optional[dict] = None


class CharacterRead(ORMBase):
    id: str
    novel_id: str
    name: str
    role: str
    personality: str
    goal: str
    appearance: str
    catchphrase: str
    pov: str
    frequency: str
    known_info: str
    extras: dict
    created_at: str
    updated_at: str


# ================= Location =================
class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    level: str = ""
    parent_id: str = ""
    description: str = ""


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    level: Optional[str] = None
    parent_id: Optional[str] = None
    description: Optional[str] = None


class LocationRead(ORMBase):
    id: str
    novel_id: str
    name: str
    level: str
    parent_id: str
    description: str
    created_at: str
    updated_at: str


# ================= WorldSetting =================
class WorldSettingCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    category: str = ""
    description: str = ""
    tags: list = Field(default_factory=list)


class WorldSettingUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list] = None


class WorldSettingRead(ORMBase):
    id: str
    novel_id: str
    name: str
    category: str
    description: str
    tags: list
    created_at: str
    updated_at: str


# ================= TimelineEvent =================
class TimelineEventCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    time_point: str = ""
    participants: list = Field(default_factory=list)
    cause_id: str = ""
    description: str = ""


class TimelineEventUpdate(BaseModel):
    title: Optional[str] = None
    time_point: Optional[str] = None
    participants: Optional[list] = None
    cause_id: Optional[str] = None
    description: Optional[str] = None


class TimelineEventRead(ORMBase):
    id: str
    novel_id: str
    title: str
    time_point: str
    participants: list
    cause_id: str
    description: str
    created_at: str
    updated_at: str


# ================= Foreshadowing =================
class ForeshadowingCreate(BaseModel):
    content: str = Field(..., min_length=1)
    status: ForeshadowStatus = ForeshadowStatus.PLANTED
    related_characters: list = Field(default_factory=list)
    planted_chapter_id: str = ""
    consumed_chapter_id: str = ""


class ForeshadowingUpdate(BaseModel):
    content: Optional[str] = None
    status: Optional[ForeshadowStatus] = None
    related_characters: Optional[list] = None
    planted_chapter_id: Optional[str] = None
    consumed_chapter_id: Optional[str] = None


class ForeshadowingRead(ORMBase):
    id: str
    novel_id: str
    content: str
    status: str
    related_characters: list
    planted_chapter_id: str
    consumed_chapter_id: str
    created_at: str
    updated_at: str


# ================= Relationship =================
class RelationshipCreate(BaseModel):
    subject_id: str = Field(..., min_length=1)
    object_id: str = Field(..., min_length=1)
    relation_type: str = Field(..., min_length=1, max_length=50)
    note: str = ""


class RelationshipUpdate(BaseModel):
    subject_id: Optional[str] = None
    object_id: Optional[str] = None
    relation_type: Optional[str] = None
    note: Optional[str] = None


class RelationshipRead(ORMBase):
    id: str
    novel_id: str
    subject_id: str
    object_id: str
    relation_type: str
    note: str
    created_at: str
    updated_at: str


# ================= Chapter =================
class ChapterCreate(BaseModel):
    title: str = ""
    seq: Optional[int] = None
    content: str = ""
    status: ChapterStatus = ChapterStatus.DRAFT
    directive: str = ""
    outline_node_id: str = ""


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[ChapterStatus] = None
    summary: Optional[str] = None
    directive: Optional[str] = None
    outline_node_id: Optional[str] = None


class ChapterRead(ORMBase):
    id: str
    novel_id: str
    outline_node_id: str
    seq: int
    title: str
    content: str
    status: str
    summary: str
    tension_score: Optional[float]
    directive: str
    created_at: str
    updated_at: str


# ================= 写作 / 定稿 / 一致性 =================
class WriteChapterRequest(BaseModel):
    title: str = ""
    directive: str = ""
    outline_node_id: str = ""


class ContinueChapterRequest(BaseModel):
    directive: str = ""


class RewritePassageRequest(BaseModel):
    passage: str = Field(..., min_length=1)
    intent: str = ""


class SettleApplyRequest(BaseModel):
    suggestions: list[dict] = Field(default_factory=list)


class ConsistencyRequest(BaseModel):
    content: str = Field(..., min_length=1)


# ================= Outline =================
class OutlineNodeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    level: OutlineLevel = OutlineLevel.CHAPTER
    summary: str = ""
    parent_id: str = ""


class OutlineNodeUpdate(BaseModel):
    title: Optional[str] = None
    level: Optional[OutlineLevel] = None
    summary: Optional[str] = None
    parent_id: Optional[str] = None


class OutlineNodeRead(ORMBase):
    id: str
    novel_id: str
    level: str
    title: str
    summary: str
    parent_id: str
    sort_order: int
    created_at: str
    updated_at: str


class OutlineReorderRequest(BaseModel):
    ordered_ids: list[str] = Field(..., min_length=1)


class AutopilotInjectRequest(BaseModel):
    directive: str = Field(..., min_length=1)


class IdeaChatRequest(BaseModel):
    text: str = Field(..., min_length=1)


class IdeaApplyRequest(BaseModel):
    suggestions: list[dict] = Field(default_factory=list)
    text: str = ""


# ================= 全局对话 =================
class ConversationCreate(BaseModel):
    novel_id: Optional[str] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    novel_id: Optional[str] = None


class ConversationRead(ORMBase):
    id: str
    novel_id: Optional[str]
    title: str
    created_at: str
    updated_at: str


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1)


class MessageUpdate(BaseModel):
    meta: Optional[dict] = None


class MessageRead(ORMBase):
    id: str
    conversation_id: str
    role: str
    content: str
    meta: Optional[dict]
    created_at: str


class PromoteRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    genre: str = ""
    summary: str = ""
