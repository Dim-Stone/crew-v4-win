"""资料预览接口。

为什么要有它：资料模式下，**"我到底读到了什么"是用户最该先看到的东西**。
一份扫描件、一份加密 PDF、或者只是放错了一个文件夹，都会让后面的章节编排
建在空地上——而那时用户看到的只是"编排出来的东西不太对"，
很难反推到"其实一份都没读进去"。

所以开课之前就能先解析一次，把读到的文件、页数与问题摆出来。

这里是**同步**接口（不像授课那样走 Turn）：解析是纯本地的，
不调模型，几十份文件也就几百毫秒。放进后台反而让用户多等一次往返。
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request

from ..services import materials_service
from ._common import fail, ok

bp = Blueprint("materials", __name__, url_prefix="/api/materials")

# 预览时最多看多少个文件。
# 有这个上限是因为用户完全可能填成一个盘符根目录（D:\），
# 那会把整块盘的 PDF 都扫一遍——既慢又不合理。
PREVIEW_LIMIT = 300


@bp.post("/preview")
def preview():
    """解析一个资料文件夹，回报"读到了什么"。**不调用模型**。"""
    path = (request.get_json(silent=True) or {}).get("path") or ""
    path = path.strip()
    if not path:
        return fail("请先填资料文件夹的路径")

    folder = Path(path)
    if not folder.is_dir():
        return fail(f"文件夹不存在或不是一个目录：{path}")

    reports = materials_service.parse_folder(folder, limit=PREVIEW_LIMIT)
    if not reports:
        return fail(
            "这个文件夹里没有找到可读的资料。支持的格式：PDF、PPTX、DOCX、TXT、MD。"
        )

    stats = materials_service.summarize_reports(reports)
    return ok({
        "stats": stats,
        "truncated_list": len(reports) >= PREVIEW_LIMIT,
        # 只回文件名与块数，不回正文：预览要的是"读到了什么"，
        # 正文可能上百 KB，塞进响应里没有意义。
        "files": [
            {
                "name": report.path.name,
                "blocks": len(report.blocks),
                "error": report.error,
                "scanned_pages": report.scanned_pages,
                "truncated": report.truncated,
            }
            for report in reports
        ],
    })
