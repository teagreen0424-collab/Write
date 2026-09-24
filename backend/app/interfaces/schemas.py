"""接口层：请求/响应模型（Pydantic）。"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.entities import (
    ChapterStatus,
    ForeshadowStatus,
    OutlineLevel,
    RevisionSource,
)


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
    # 仅用于接口编排：决定覆盖前快照的来源，不落库到 chapter 表。
    # 旧调用不传时保持兼容，默认按手工保存留档；只接受 manual_save / rewrite_apply。
    revision_source: RevisionSource = RevisionSource.MANUAL_SAVE


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
    drift_score: Optional[float]
    directive: str
    created_at: str
    updated_at: str


# ================= 章节版本（阶段八第一轮） =================
class ChapterRevisionMeta(ORMBase):
    """版本列表项：不返回完整正文，避免一次拉回整章历史。"""

    id: str
    novel_id: str
    chapter_id: str
    revision_no: int
    source: str
    title: str
    directive: str
    status: str
    summary: str
    tension_score: Optional[float]
    drift_score: Optional[float]
    word_count: int
    created_at: str


class ChapterRevisionRead(ChapterRevisionMeta):
    """单个版本详情：包含完整正文。"""

    content: str


# ================= 成品交付（阶段八第二轮） =================
class DeliveryRequest(BaseModel):
    format: Literal["markdown", "txt", "docx"] = "markdown"
    scope: Literal["all", "volume", "chapters"] = "all"
    volume_id: str = ""
    chapter_ids: list[str] = Field(default_factory=list)
    include_drafts: bool = False


# ================= 写作 / 定稿 / 一致性 =================
class WriteChapterRequest(BaseModel):
    title: str = ""
    directive: str = ""
    outline_node_id: str = ""
    previous_chapter_id: str = ""


class ContinueChapterRequest(BaseModel):
    directive: str = ""


class RewritePassageRequest(BaseModel):
    passage: str = Field(..., min_length=1)
    intent: str = ""
    preview: bool = False


class SettleApplyRequest(BaseModel):
    suggestions: list[dict] = Field(default_factory=list)


class ConsistencyRequest(BaseModel):
    content: str = Field(..., min_length=1)


# ================= 质量治理（阶段七） =================
class ClicheRequest(BaseModel):
    content: str = Field(..., min_length=1)


class ClicheHit(BaseModel):
    term: str
    category: str
    start: int
    end: int


class ClicheResponse(BaseModel):
    hits: list[ClicheHit]
    total: int
    density: float
    by_category: dict[str, int]


class QualityCurvePoint(BaseModel):
    seq: int
    title: str
    status: str
    tension_score: Optional[float]
    drift_score: Optional[float]


class QualityCurveResponse(BaseModel):
    chapters: list[QualityCurvePoint]
    anchor_set: bool
    drift_threshold: float


class ForeshadowingTrack(ORMBase):
    id: str
    content: str
    status: str
    related_characters: list
    planted_chapter_id: str
    consumed_chapter_id: str
    planted_seq: Optional[int]
    suspended_chapters: Optional[int]
    long_pending: bool


class ForeshadowingGroups(BaseModel):
    planted: list[ForeshadowingTrack]
    suspended: list[ForeshadowingTrack]
    consumed: list[ForeshadowingTrack]


class LongPendingThreshold(BaseModel):
    chapters: int
    ratio: float


class ForeshadowingTrackingResponse(BaseModel):
    groups: ForeshadowingGroups
    latest_seq: int
    chapter_count: int
    long_pending_threshold: LongPendingThreshold


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


class OutlineChapterDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""


class OutlineActDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    chapters: list[OutlineChapterDraft] = Field(default_factory=list, max_length=20)


class OutlineVolumeDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    acts: list[OutlineActDraft] = Field(default_factory=list, max_length=8)
    chapters: list[OutlineChapterDraft] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_tree(self):
        if not self.title.strip():
            raise ValueError("卷标题不能为空")
        if any(not act.title.strip() for act in self.acts):
            raise ValueError("幕标题不能为空")
        chapters = self.chapters + [chapter for act in self.acts for chapter in act.chapters]
        if not chapters or any(not chapter.title.strip() for chapter in chapters):
            raise ValueError("至少需要一章，且章节标题不能为空")
        return self


class OutlinePreviewRequest(BaseModel):
    source_text: str = Field(..., min_length=1, max_length=8000)


class OutlineApplyRequest(BaseModel):
    plan: OutlineVolumeDraft


class AutopilotInjectRequest(BaseModel):
    directive: str = Field(..., min_length=1)


class IdeaChatRequest(BaseModel):
    text: str = Field(..., min_length=1)


class IdeaApplyRequest(BaseModel):
    suggestions: list[dict] = Field(default_factory=list)
    raw_text: str = ""
    channel: str = "chat"
    # 兼容旧客户端曾使用的 text 字段；新调用统一发送 raw_text。
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
