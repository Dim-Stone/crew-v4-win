"""数据模型。

一期是**单用户本地模式**（不做登录），所以没有 users 表，
画像直接以 id=1 的单行形式存在 learner_profiles 里。

整体结构：
    Course(学习方向) → Chapter(章节) → KnowledgePoint(知识点)
    StudySession(学习会话) → Message(对话消息) / Turn(回合任务)
    LearnerProfile(用户画像) + ProfileEvent(画像演化流水)
"""

from datetime import datetime, timezone
import json

from .extensions import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def json_list(raw: str | None) -> list:
    """把"存在 Text 列里的 JSON 数组"安全地读成 list。

    为什么需要它（评审 3.4）：原来 `Message.to_dict()` 直接
    `json.loads(self.tags or "[]")`。历史数据里只要有一条 `tags` 是坏 JSON，
    **整个会话历史接口就 500**——一条脏数据毁掉一屏内容。
    这里的原则是"读不出来就当空列表"：标签是辅助信息，不该有否决权。

    非数组的合法 JSON（`{"a":1}`、`"x"`）也当空列表：调用方按 list 用，
    给它一个 dict 只会在下游某处炸掉。
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


class LearnerProfile(db.Model):
    """用户画像（单用户，固定 id=1）。

    摸底调查的结果落在这里，之后每一轮教学都会被摘要后注入提示词，
    从而影响智能体输出的形式与风格。
    """

    __tablename__ = "learner_profiles"

    id = db.Column(db.Integer, primary_key=True)
    # 学习能力自评 + LLM 判断
    ability_level = db.Column(db.String(32), default="unknown")  # beginner/intermediate/advanced
    # 兴趣点（逗号分隔的自由文本，一期不做多表）
    interests = db.Column(db.Text, default="")
    # 学习目的：应试 / 兴趣 / 打基础 / 拓展
    purpose = db.Column(db.Text, default="")
    # 期望的授课风格描述
    style_preference = db.Column(db.Text, default="")
    # 讲法节奏：faster / normal / slower
    # 由"反馈—自适应"闭环调整，且**每次只挪一档**（见 profile_service._shift_pace）。
    # 为什么单独一个字段而不是改写 style_preference：
    #   style_preference 是摸底时生成的自然语言描述，无法做"渐进"——只能整体替换，
    #   而整体替换正是画像震荡的来源。这个字段是枚举，天然只能一格一格挪。
    pace = db.Column(db.String(16), default="normal")
    # 可用于学习的时间
    time_budget = db.Column(db.Text, default="")
    # 摸底问卷的原始回答（JSON 字符串，保留证据）
    raw_intake = db.Column(db.Text, default="{}")
    # 画像的可读摘要，直接拼进提示词
    summary_md = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    @property
    def is_filled(self) -> bool:
        return bool(self.summary_md or self.ability_level != "unknown")


class ProfileEvent(db.Model):
    """画像演化流水。

    每一轮教学结束后由后置解析写入一条，
    它本身是"画像如何随学习变化"的可回溯证据（可写进研究报告）。
    """

    __tablename__ = "profile_events"

    id = db.Column(db.Integer, primary_key=True)
    turn_id = db.Column(db.Integer, db.ForeignKey("turns.id"), nullable=True)
    # 本回合观察到的信号（JSON 字符串）
    signals = db.Column(db.Text, default="{}")
    note = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_now)


class Course(db.Model):
    """一个学习方向 / 课程。"""

    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    # 用户输入的方向描述，如"环境工程导论"
    direction = db.Column(db.Text, default="")
    # 用户补充的学习重点
    focus = db.Column(db.Text, default="")
    # 资料模式："" = 不使用资料（沿用原有流程）；
    # "strict" / "reference" = 依据强度（见 app/materials.py）。
    material_mode = db.Column(db.String(16), default="")
    # 资料所在目录（本机路径）。**只存路径，不复制文件**——
    # 用户的课件动辄几百 MB，复制一份既慢又白占磁盘；
    # 而且他只改了原件，我们下次读到的就是最新版。
    material_path = db.Column(db.Text, default="")
    # 大纲生成状态：draft(待生成) / ready(已生成)
    status = db.Column(db.String(32), default="draft")
    # 大纲版本：每次"重新生成大纲"加一。
    # **为什么需要它**（评审 3.2/不足.md 第八条）：重新生成会删掉旧章节与知识点，
    # 于是"学过的历史属于哪一版大纲"就说不清了。有了版本号，至少能回答
    # "这条会话/这份讲义是在第几版大纲下产生的"；将来接掌握度时，
    # 它是"新旧知识点能不能继承"的判据。
    outline_version = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_now)

    chapters = db.relationship(
        "Chapter",
        backref="course",
        cascade="all, delete-orphan",
        order_by="Chapter.order_index",
    )


class Chapter(db.Model):
    __tablename__ = "chapters"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    summary = db.Column(db.Text, default="")
    order_index = db.Column(db.Integer, default=0)

    points = db.relationship(
        "KnowledgePoint",
        backref="chapter",
        cascade="all, delete-orphan",
        order_by="KnowledgePoint.order_index",
    )


class KnowledgePoint(db.Model):
    __tablename__ = "knowledge_points"

    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey("chapters.id"), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    summary = db.Column(db.Text, default="")
    difficulty = db.Column(db.String(32), default="medium")  # easy/medium/hard
    est_minutes = db.Column(db.Integer, default=15)
    # 前置知识点标题（JSON 数组字符串，一期只作展示与提示）
    prerequisites = db.Column(db.Text, default="[]")
    # 资料模式下：这个知识点依据的材料位置（JSON 数组字符串，
    # 例如 ["矩阵引论.pdf 第 2 页"]）。
    # **空数组是有意义的**——它表示"资料里没有这一段，是模型补充的"。
    # 参考模式下允许出现，严格模式下不该出现；界面上要如实区分，
    # 否则学生没法判断哪些内容能回原文核对。
    sources = db.Column(db.Text, default="[]")
    order_index = db.Column(db.Integer, default=0)
    # 讲授进度：pending / teaching / done
    status = db.Column(db.String(32), default="pending")


class LessonPlan(db.Model):
    """某个知识点的「教案」——讲师的备课产物。

    为什么需要它：
        原先讲师每轮都是**即兴装配**（大纲 + 当前知识点标题 + 最近几条对话）：
        既不知道"这一节分几步讲、现在讲到第几步"，也没有"已经讲过什么"的记录。
        结果就是重复讲同一段、前后不连贯。

        把备课写在数据库里（也就是上下文之外），每轮按需拉回来——
        这正是 Anthropic 说的 structured note-taking：
        计划与进度留在外部，上下文里只放当下需要的那一份。
    """

    __tablename__ = "lesson_plans"

    id = db.Column(db.Integer, primary_key=True)
    point_id = db.Column(
        db.Integer, db.ForeignKey("knowledge_points.id"), nullable=False, unique=True
    )
    point = db.relationship(
        "KnowledgePoint",
        backref=db.backref("lesson_plan", uselist=False, cascade="all, delete-orphan"),
    )

    # 学完这个点，学习者应该能做到什么（一句话）
    goal = db.Column(db.Text, default="")
    # JSON: [{"title": 步骤名, "intent": 这一步要达成什么,
    #         "key_points": [要点...], "example": 这一步用的例子}]
    steps = db.Column(db.Text, default="[]")
    # JSON: 预见的常见错误
    pitfalls = db.Column(db.Text, default="[]")
    # JSON: 检验问题
    checks = db.Column(db.Text, default="[]")

    # 讲到第几步（0 起）；steps 走完即认为这个点讲完
    current_step = db.Column(db.Integer, default=0)
    # 当前这一步已经讲了几轮。用于**硬性兜底**：同一步骤讲太久就强制往前走，
    # 不依赖模型判断（实测模型的"保守"会把进度彻底锁死）。
    step_rounds = db.Column(db.Integer, default=0)
    # 已讲要点的累积摘要。同一知识点讲多轮时，靠它避免重复——
    # 等价于把历史"压缩"成一段高信号文本，而不是把整段对话全塞回去。
    covered = db.Column(db.Text, default="")

    # ---- 卡片课堂的环节状态（P0）----
    #
    # 为什么必须**显式落库**，而不是从页面推断：
    #   · 刷新/断线/超时之后要能恢复到"这一节进行到哪个环节"；
    #   · "跳过测试"与"测过了"必须区分开——前者没有掌握度证据。
    # 取值：lecturing / quizzing / grading / reviewing / done
    stage = db.Column(db.String(32), default="lecturing")
    # 整节复盘做过的时间。复盘从"每轮一次"改成"讲完这一节一次"之后，
    # 需要它保证**只做一次**（否则每次进入课堂都会再花一次模型调用）。
    reviewed_at = db.Column(db.DateTime, nullable=True)
    stage_started_at = db.Column(db.DateTime, nullable=True)
    # 这一节的小测被跳过了吗？跳过**不等于通过**：不产生掌握度证据，
    # 界面显示灰色"未测"，讲师下一轮备课也会被告知"这节没测过"。
    quiz_skipped = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def steps_list(self) -> list[dict]:
        import json

        try:
            data = json.loads(self.steps or "[]")
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def is_finished(self) -> bool:
        steps = self.steps_list()
        return bool(steps) and self.current_step >= len(steps)


class LessonPage(db.Model):
    """一"页"讲授内容——卡片课堂的展开单位。

    ## 为什么要有它

    原先讲授内容只以 `Message` 的形式存在（一条气泡一段话）。要把它做成
    "一页一卡"的课堂，就必须有一个**可寻址、可排序、带状态**的对象：
    翻页要按 index、刷新要按状态恢复、跳过要知道跳过的是哪一页。

    ## 一页从哪来

    P0 的做法是**确定性的**：讲师仍按原来的方式生成整段讲授，
    写完消息后由 `page_builder` 按标题/段落把它切成若干页
    （不在代码块、公式、表格内部切）。所以"一条讲师消息 = 1..n 页"，
    `turn_id` 指回产生它的那一轮，`step_index` 记录它对应教案第几步。

    ## 为什么 `index` 是"知识点内"的序号

    卡片是"一个知识点一组页"。跨知识点混排会让"第 3/8 页"这种提示失去意义，
    也让"跳到上一页"在知识点边界处产生歧义。所以序号只在 `point_id` 内递增，
    由 `page_service` 在同一事务里分配（不靠自增 id 猜顺序）。
    """

    __tablename__ = "lesson_pages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("study_sessions.id"), nullable=False
    )
    point_id = db.Column(
        db.Integer, db.ForeignKey("knowledge_points.id"), nullable=True
    )
    # 产出这一页的教案（知识点可能没有教案——备课失败时仍要能上课）
    plan_id = db.Column(db.Integer, db.ForeignKey("lesson_plans.id"), nullable=True)
    # 这一页对应教案的第几步（0 起）；对不上时为 None
    step_index = db.Column(db.Integer, nullable=True)

    # 在**该知识点内**的页序，从 0 开始
    index = db.Column(db.Integer, default=0)

    # 目前只有 lecture；quiz / grade / review 留给 P1，取值先不约束
    # （用字符串而不是枚举：加一类页不该改表结构）
    kind = db.Column(db.String(24), default="lecture")
    title = db.Column(db.String(200), default="")
    content = db.Column(db.Text, default="")

    # running（正在生成）/ ready / failed / skipped（用户跳过本页）
    status = db.Column(db.String(24), default="ready")
    # 同一页被"再讲一遍"了几次（P2 用；P0 只记录，不影响流程）
    attempts = db.Column(db.Integer, default=0)

    turn_id = db.Column(db.Integer, db.ForeignKey("turns.id"), nullable=True)
    # 这一页真正用到的出处（JSON 数组），来自讲授那一轮
    materials = db.Column(db.Text, default="[]")

    created_at = db.Column(db.DateTime, default=_now)

    def materials_list(self) -> list[str]:
        value = json_list(self.materials)
        return [str(item) for item in value]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "step_index": self.step_index,
            "attempts": self.attempts or 0,
            "materials": self.materials_list(),
            "point_id": self.point_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Quiz(db.Model):
    """一次小测：一个知识点一次，挂在产生它的那一轮任务上。

    ## 为什么题面存在 `payload` 里，而不是每题一行

    一次小测的题是一个**整体**：一起生成、一起判、一起进掌握度。
    拆成 `QuizItem` 表会多一次 join，却换不来任何查询（没有"跨小测查某道题"的需求）。
    逐题的**作答**倒是单独一张表——因为那才是会被统计的东西。

    ## 为什么 `status` 里必须有 `skipped`

    "跳过测试"不等于"通过"：跳过没有分数、不产生掌握度证据，
    界面上显示为"未测"。把两者混在一起，掌握度会平白多出一批假证据。
    """

    __tablename__ = "quizzes"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("study_sessions.id"), nullable=False
    )
    point_id = db.Column(
        db.Integer, db.ForeignKey("knowledge_points.id"), nullable=True
    )
    # 哪一页的"讲完之后"出的题（允许为空：也可能从工具栏手动出题）
    page_id = db.Column(db.Integer, db.ForeignKey("lesson_pages.id"), nullable=True)

    # ---- 章末测试 ----
    # 归属哪一章；scope 区分"每节小测"与"章末测试"。
    # 章末测试的题目按**知识点**标注（而不是第几步），因此判分后能聚合出
    # "这一章哪个知识点薄弱"——这是它比小测更有价值的地方。
    chapter_id = db.Column(db.Integer, db.ForeignKey("chapters.id"), nullable=True)
    scope = db.Column(db.String(16), default="point")
    # 按知识点聚合的得分（JSON: {point_id: {right, total, score}}）
    aggregate = db.Column(db.Text, default="{}")
    turn_id = db.Column(db.Integer, db.ForeignKey("turns.id"), nullable=True)

    # 题面（JSON）：{"items": [...]}，含答案与解析；发给前端时会剥掉答案
    payload = db.Column(db.Text, default="{}")
    # 这次要了几题、要了哪些题型（JSON 数组）——重出题时要沿用
    wanted_count = db.Column(db.Integer, default=5)
    types = db.Column(db.Text, default="[]")

    # open（待作答）/ graded（已判）/ skipped（跳过）
    status = db.Column(db.String(16), default="open")
    # 0..1；skipped 时为 None
    score = db.Column(db.Float, nullable=True)
    # 错在哪几步（JSON 数组），下一次讲什么就是靠它
    weak_steps = db.Column(db.Text, default="[]")

    created_at = db.Column(db.DateTime, default=_now)
    graded_at = db.Column(db.DateTime, nullable=True)

    def items(self) -> list[dict]:
        try:
            data = json.loads(self.payload or "{}")
        except (TypeError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def aggregate_dict(self) -> dict:
        """按知识点聚合的得分。"""
        try:
            value = json.loads(self.aggregate or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def to_dict(self, *, include_answers: bool = False) -> dict:
        items = []
        for item in self.items():
            one = dict(item)
            if not include_answers:
                # 判分之前绝不能把答案发到前端：那不是"防作弊"，
                # 而是别让一次误判的刷新把答案泄漏进页面历史上。
                one.pop("answer", None)
            items.append(one)
        return {
            "id": self.id,
            "session_id": self.session_id,
            "point_id": self.point_id,
            "page_id": self.page_id,
            "kind_counts": self.wanted_count,
            "types": json_list(self.types),
            "status": self.status,
            "score": self.score,
            "weak_steps": json_list(self.weak_steps),
            "chapter_id": self.chapter_id,
            "scope": self.scope or "point",
            "aggregate": self.aggregate_dict(),
            "items": items,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "graded_at": self.graded_at.isoformat() if self.graded_at else None,
        }


class QuizAnswer(db.Model):
    """一次作答里的一道题。

    单独一张表是为了**能统计**：哪一步老错、哪种题型老错、哪道题全班都错
    （将来有多个学习者时）。这些都靠逐题记录，靠 JSON 里的总分做不到。
    """

    __tablename__ = "quiz_answers"

    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey("quizzes.id"), nullable=False)
    item_id = db.Column(db.String(24), default="")
    item_type = db.Column(db.String(24), default="")
    # 对应教案第几步（0 起）；null = 综合题
    step = db.Column(db.Integer, nullable=True)
    # 学生答的（选择题是键，填空题是原文）
    response = db.Column(db.Text, default="")
    correct = db.Column(db.Boolean, default=False)
    score = db.Column(db.Float, default=0.0)
    # blank / careless / concept_confusion / off_topic
    error_kind = db.Column(db.String(32), default="")
    elapsed_ms = db.Column(db.Integer, nullable=True)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "step": self.step,
            "response": self.response,
            "correct": bool(self.correct),
            "score": self.score or 0.0,
            "error_kind": self.error_kind,
        }


class StudySession(db.Model):
    """一次学习会话：绑定课程，记录当前推进到哪个知识点。"""

    __tablename__ = "study_sessions"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    # 指向"当前讲到哪"。声明成外键是为了让 `_ensure_missing_columns` 之后的
    # 语义与代码一致；**清理仍靠代码**（重规划时要显式修复指针，见
    # `curriculum_service.replan_outline`）。
    current_chapter_id = db.Column(db.Integer, db.ForeignKey("chapters.id"), nullable=True)
    current_point_id = db.Column(
        db.Integer, db.ForeignKey("knowledge_points.id"), nullable=True
    )
    # active / finished / archived
    # `archived` 是"重新生成大纲"时给旧会话留的归宿：历史对话不删，
    # 但不再被自动复用（否则新旧大纲的消息会混在一条时间轴里）。
    status = db.Column(db.String(32), default="active")
    created_at = db.Column(db.DateTime, default=_now)

    course = db.relationship("Course")
    messages = db.relationship(
        "Message",
        backref="session",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("study_sessions.id"), nullable=False)
    role = db.Column(db.String(16), nullable=False)  # user / agent
    # 哪个智能体产出的：lecturer / visual / system
    agent = db.Column(db.String(32), default="")
    content = db.Column(db.Text, default="")
    # 便于时间轴视图的标签（JSON 字符串）
    tags = db.Column(db.Text, default="[]")
    # ---- 关联（评审 3.1：靠标签推断"这条消息属于哪个知识点"太脆）----
    #
    # `course_id` / `point_id`：这条消息属于哪门课、哪个知识点。
    #   以前只靠 `tags` 里的「知识点：xxx」文本推断，于是
    #     · 切换知识点后、图解取"最近一条讲师消息"会拿到上一个点的内容；
    #     · 用户消息的 tags 是 "[]"，历史分组时问答会被拆到两个组里。
    # `turn_id`：产生它的那一轮任务。以前是普通整数列且从没被填过，
    #   所以"这轮教学产生了哪些信号"根本追溯不了。
    #
    # 为什么用 nullable=True：历史数据没有这些值，加列时必须允许空；
    # 读取端（图解、历史分组）对空值有回退逻辑（见 teaching_service）。
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=True)
    point_id = db.Column(db.Integer, db.ForeignKey("knowledge_points.id"), nullable=True)
    turn_id = db.Column(db.Integer, db.ForeignKey("turns.id"), nullable=True)
    # 这条消息属于哪一"页"。卡片课堂里，页内答疑的气泡挂在页上；
    # 老消息为 NULL（它们没有页，或者由迁移 v2 回填时指向该轮的第一页）。
    page_id = db.Column(db.Integer, db.ForeignKey("lesson_pages.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "agent": self.agent,
            "content": self.content,
            "tags": json_list(self.tags),
            "turn_id": self.turn_id,
            "course_id": self.course_id,
            "point_id": self.point_id,
            "page_id": self.page_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Turn(db.Model):
    """一次异步执行（一个教学回合 / 一次大纲生成 / 一次图解）。

    这是"防阻塞"的核心：请求只创建 Turn 并立即返回，
    真正的耗时执行在线程池里进行，前端通过 SSE 订阅它的增量输出。
    """

    __tablename__ = "turns"

    id = db.Column(db.Integer, primary_key=True)
    # 两种归属：授课/图解挂会话（同时也能推出课程），大纲挂课程。
    # 声明 ForeignKey 是为了表达"这是一条引用"，但**真正的清理靠代码**：
    # SQLite 默认不开外键约束（PRAGMA foreign_keys），所以删除课程时
    # 仍必须显式清任务（见 curriculum_service.delete_course）。
    session_id = db.Column(db.Integer, db.ForeignKey("study_sessions.id"), nullable=True)
    # 大纲类回合挂在课程上（授课类用 session_id）。有了它，
    # 课程列表才能一眼看出"这门课现在有没有正在跑的规划"。
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=True)
    # plan / teach / diagram / outline / intake
    kind = db.Column(db.String(32), default="teach")
    # pending / running / done / failed / cancelled
    # `cancelled` 与 `failed` 分开：一个是用户主动中止（不是故障，不该计入
    # 失败率、也不该在界面上报红），一个是真的出了问题。
    status = db.Column(db.String(32), default="pending")
    # 当前阶段的人话描述（"正在读取资料文件夹"、"读资料 3/12：xxx.pdf"）。
    # 由 runtime 在每次 status 事件时写回数据库——**这一步是"看得见"的关键**：
    # 用户随时可能离开页面，回来时只能从库里查进度，而查不到当时那条 SSE。
    stage = db.Column(db.String(200), default="")
    # 输入参数（JSON 字符串）
    payload = db.Column(db.Text, default="{}")
    # 累积的产出全文（流结束后写满）
    result = db.Column(db.Text, default="")
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_now)
    finished_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            # `stage` 是"跑到哪一步了"的人话描述。前端断线重连时先看它，
            # 比只看 status=running 有信息量得多（"读资料 3/12" vs "正在跑"）。
            "stage": self.stage or "",
            "session_id": self.session_id,
            "course_id": self.course_id,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TodoItem(db.Model):
    """主页"今天"卡片里的一条待办。

    第一版只做**显示 + 新增**，当时的理由是"不留半成品的字段"——`done` 这类列
    一旦加上，界面与接口都要跟着给出说法。**后来用户要了完成态与按日期过滤**，
    所以这里补上 `done` 与 `target_date`：那条理由依然成立，只是现在有说法了
    （勾选后加删除线、按选中日期取清单，见 `services/todo_service.py`）。

    `target_date` 是"**这一天要做的事**"，不是截止日期：切到某天看到的就是那一天
    自己的清单，空白日期就是空白。用 `db.Date` 而不是 `DateTime`——界面只精确到
    天，存了时间会让"同一天"退化成字符串比较。

    **旧数据**（升级前建的）这两列都是 NULL：`done` 按 False 读（`bool()` 兜住），
    `target_date` 为 NULL 的行会被"按日期取"的查询漏掉。**刻意不做数据回填**——
    把一个"当初随手记的一笔"硬钉在升级那天，比让它留在库里更难解释。

    为什么单独一张表、也不挂在 Course 上：它的生命周期与学习数据无关。
    删课程不该动到待办，反之亦然。
    """

    __tablename__ = "todo_items"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    #: 勾上了就画一条删除线（不删行——"我做过了什么"本身是信息）
    done = db.Column(db.Boolean, nullable=False, default=False)
    #: 归属哪一天。索引是给"按日期取清单"用的，那是这一页最主要的查询。
    target_date = db.Column(db.Date, index=True)
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            # `bool()` 兜住老库里补出来的 NULL（那个列是可空的，见类注释）
            "done": bool(self.done),
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DayNote(db.Model):
    """某一天的"记事本"：想写什么就写什么，与课程无关。

    为什么与待办**分两张表**：它们的生命周期不同——待办是一条条可勾掉的事项，
    记事本是一整段随时会被整段重写的文字。合成一张表的话，"清空这段文字"和
    "删掉所有待办"就变成同一个动作了。

    主键用**日期本身**而不是自增 id：一个日期只有一份记事本，用它当键天然唯一；
    而且界面手里本来就是日期，不必先查 id 再读。

    没有行 = 那天还没写过（读的时候返回一份空的，**不建行**——看一眼不该写库）。
    """

    __tablename__ = "day_notes"

    date = db.Column(db.Date, primary_key=True)
    text = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat() if self.date else None,
            "text": self.text or "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
