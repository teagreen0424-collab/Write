"""接口层：请求/响应模型（Pydantic）。"""
from typing import Annotated, Literal, Optional
from uuid import UUID

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
    request_id: Optional[UUID] = None
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


class ChapterCreationRead(ChapterRead):
    creation_replayed: bool
    author_changed_since_creation: bool


class ChapterMetadata(ORMBase):
    """章节目录投影，不包含正文、摘要或章前指令。"""

    id: str
    novel_id: str
    seq: int
    title: str
    status: str
    outline_node_id: str
    created_at: str
    updated_at: str
    tension_score: Optional[float]
    drift_score: Optional[float]
    word_count: int


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


class OutlineCheckRequest(BaseModel):
    content: Optional[str] = None


class OutlineCheckIssue(BaseModel):
    kind: Literal["missing", "changed", "contradiction"]
    severity: Literal["high", "medium", "low"]
    outline_point: str = Field(..., min_length=1, max_length=500)
    excerpt: str = Field(default="", max_length=300)
    reason: str = Field(..., min_length=1, max_length=800)


class OutlineCheckResult(BaseModel):
    issues: list[OutlineCheckIssue] = Field(default_factory=list, max_length=6)
    truncated: bool = False
    outline_title: str = ""


class WordCountLimits(BaseModel):
    """作者显式选择的每章字数范围；不从自然语言设定推测硬限制。"""

    model_config = ConfigDict(extra="forbid")
    min_words: Optional[int] = Field(default=None, ge=1, le=1_000_000, strict=True)
    max_words: Optional[int] = Field(default=None, ge=1, le=1_000_000, strict=True)

    @model_validator(mode="after")
    def validate_range(self):
        if self.min_words is None and self.max_words is None:
            raise ValueError("字数范围至少填写一个边界")
        if self.min_words is not None and self.max_words is not None and self.min_words > self.max_words:
            raise ValueError("最少字数不能大于最多字数")
        return self


class WordCountResult(BaseModel):
    count: int = Field(..., ge=0)
    min_words: Optional[int] = None
    max_words: Optional[int] = None
    within_limits: Optional[bool] = None
    warnings: list[str] = Field(default_factory=list)


class StoryRequirement(BaseModel):
    id: str
    source: Literal["directive", "outline"]
    text: str


class StoryRequirementCheck(BaseModel):
    requirement_id: str
    status: Literal["violation", "satisfied", "uncertain"]
    excerpt: str = ""
    reason: str


class StoryCheckRequest(BaseModel):
    content: Optional[str] = None
    directive: Optional[str] = None
    word_limits: Optional[WordCountLimits] = None


class StoryCheckIssue(BaseModel):
    category: Literal["directive", "continuity", "setting", "outline"]
    severity: Literal["high", "medium", "low"]
    requirement: str = Field(..., min_length=1, max_length=800)
    excerpt: str = Field(default="", max_length=400)
    reason: str = Field(..., min_length=1, max_length=1200)


class StoryCheckResult(BaseModel):
    issues: list[StoryCheckIssue] = Field(default_factory=list, max_length=6)
    truncated: bool = False
    history_truncated: bool = False
    review_complete: bool = True
    previous_chapter_title: str = ""
    chapter_title: str = ""
    requirements: list[StoryRequirement] = Field(default_factory=list)
    requirement_checks: list[StoryRequirementCheck] = Field(default_factory=list)
    unreviewed_requirement_ids: list[str] = Field(default_factory=list)
    word_count: Optional[WordCountResult] = None


class ChapterRepairPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(default="", max_length=4000, strict=True)
    word_limits: Optional[WordCountLimits] = None
    issues: list[StoryCheckIssue] = Field(default_factory=list, max_length=6)
    unreviewed_requirement_ids: list[Annotated[str, Field(min_length=1, max_length=16, strict=True)]] = Field(default_factory=list, max_length=200)


class ChapterRepairPreview(BaseModel):
    chapter_id: str
    base_fingerprint: str
    original_content: str
    content: str
    title: str
    directive: str
    original_word_count: WordCountResult
    word_count: WordCountResult


class ChapterRepairApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$", strict=True)
    content: str = Field(..., min_length=1, max_length=40000, strict=True)


class ContinueChapterRequest(BaseModel):
    directive: str = ""


class RewritePassageRequest(BaseModel):
    passage: str = Field(..., min_length=1)
    intent: str = ""
    preview: bool = False


class SettleAuthorFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., strict=True)
    content: str = Field(..., strict=True)
    directive: str = Field(..., strict=True)


class SettleRequest(BaseModel):
    expected_author_fields: Optional[SettleAuthorFields] = None


class SettleApplyRequest(BaseModel):
    source_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$", strict=True)
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
    request_id: Optional[UUID] = None


class AutopilotTaskStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    word_limits: Optional[WordCountLimits] = None


class AutopilotTaskActiveDraft(BaseModel):
    node_id: str
    title: str
    content: str
    draft_id: str
    draft_revision: int
    draft_attempt: int
    draft_persisted: bool


class AutopilotTaskDriftWarning(BaseModel):
    seq: int
    score: float
    message: str


class AutopilotTaskSnapshot(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    task_id: str
    novel_id: str
    event_id: int
    status: Literal["starting", "running", "paused", "completed", "incomplete", "stopped", "interrupted"]
    running: bool
    paused: bool
    pause_requested: bool
    generated: int
    skipped: int
    failed: int
    done: int
    total: int
    current_node_id: str
    current_seq: int
    message: str
    task_notice: str
    terminal_reason: str
    active_drafts: list[AutopilotTaskActiveDraft] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)
    drift_warnings: list[AutopilotTaskDriftWarning] = Field(default_factory=list)
    word_limits: Optional[WordCountLimits] = None


class AutopilotDraftUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=100, strict=True)
    title: str = Field(default="", max_length=200, strict=True)
    attempt: int = Field(..., ge=1, le=2_147_483_647, strict=True)
    content: str = Field(..., max_length=1_000_000, strict=True)
    reason: str = Field(default="", max_length=4000, strict=True)
    state: Literal["active", "failed", "unconfirmed"]
    revision: int = Field(..., ge=1, le=2_147_483_647, strict=True)

    @model_validator(mode="after")
    def validate_node(self):
        if not self.node_id.strip():
            raise ValueError("草稿所属节点不能为空")
        return self


class AutopilotDraftRead(ORMBase):
    id: str
    novel_id: str
    node_id: str
    title: str
    attempt: int
    content: str
    reason: str
    state: Literal["active", "failed", "unconfirmed"]
    revision: int
    created_at: str
    updated_at: str


class AutopilotDraftDeleted(BaseModel):
    id: str
    deleted: Literal[True] = True
    revision: int


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
