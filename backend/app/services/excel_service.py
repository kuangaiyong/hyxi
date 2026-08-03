"""Excel 生成服务 - 复用 openpyxl 样式，含舆情报告导出"""

import os
import json
from datetime import datetime
from typing import Optional
from collections import Counter
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.chart import PieChart, Reference
from openpyxl.utils import get_column_letter
from app.config import settings
from app.services.post_tree import order_by_thread
from app.services.progress_manager import ProgressManager


# 复用 build_excel.py 的样式常量
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)
CONTENT_ALIGN = Alignment(vertical="top", wrap_text=True)
CENTER_ALIGN = Alignment(horizontal="center", vertical="top")
ALT_FILL = PatternFill(start_color="F2F7FB", end_color="F2F7FB", fill_type="solid")




def _star_level(value) -> int:
    """LLM 可能把 intensity 返回成字符串、None 或越界数值，星级渲染只接受 0..5"""
    if not isinstance(value, (int, float)) or value != value:
        return 0
    return round(max(0, min(5, value)))


def _parse_dutch_timestamp(ts: str):
    """帖子时间戳落盘格式为 dd-mm-yyyy HH:MM，字典序不等于时间序"""
    try:
        return datetime.strptime(ts, "%d-%m-%Y %H:%M")
    except (ValueError, TypeError):
        return None


def _save_workbook(wb, output_path: str) -> None:
    """保存工作簿。

    Windows 上用户若正用 Excel 打开同名文件，save 会抛 PermissionError，
    未捕获时前端只看到一个 500，无从判断该做什么。
    """
    try:
        wb.save(output_path)
    except PermissionError:
        raise Exception(f"无法写入 {os.path.basename(output_path)}，请先关闭已在 Excel 中打开的同名文件")


class ExcelService:
    """生成翻译 Excel 报告"""

    @staticmethod
    async def execute(
        task_id: str,
        posts: list,
        params: dict,
        progress: ProgressManager,
        step_index: int = 2,
        sources: Optional[dict] = None,
    ) -> dict:
        """生成 Excel。

        step_index 不能写死 2 —— plan 里有多个 collect 步骤时 Excel 就不在第 3 位了。
        sources 是 {source_id: {"name": ...}}，用来把来源列渲染成人看得懂的名字。
        """
        include_stats = params.get("include_stats", True)
        source_names = {sid: m.get("name", sid) for sid, m in (sources or {}).items()}
        posts = order_by_thread(posts)

        await progress.emit(task_id, "step_progress", {
            "step": step_index,
            "progress": 0.0,
            "message": "正在生成 Excel 报告...",
        })

        wb = Workbook()

        # ===== Sheet 1: 论坛帖子翻译 =====
        ws = wb.active
        ws.title = "论坛帖子翻译"

        headers = [
            ("序号", 6), ("来源", 20), ("层级", 6), ("用户名", 16), ("发布时间", 18),
            ("原文", 55), ("中文翻译", 55), ("页码", 6),
        ]
        for col, (header, width) in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGN
            cell.border = THIN_BORDER
            ws.column_dimensions[get_column_letter(col)].width = width

        ws.freeze_panes = "A2"

        for idx, post in enumerate(posts):
            row = idx + 2
            level = int(post.get("reply_level", 0) or 0)
            # 回复行统一浅底，比隔行底纹更能一眼看出层级；不用 openpyxl 的分组，
            # 各家查看器对 outline 的行为不一致
            fill = ALT_FILL if (level > 0 or idx % 2 == 1) else PatternFill()
            sid = post.get("source", "")

            values = [
                idx + 1,
                source_names.get(sid, sid),
                level,
                ("　" * level + "└─ " if level else "") + post.get("username", ""),
                post.get("timestamp", ""),
                post.get("content", ""),
                post.get("translation", ""),
                post.get("page_number", ""),
            ]
            for col, value in enumerate(values, 1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = THIN_BORDER
                cell.fill = fill
                if col in (1, 3, 8):
                    cell.alignment = CENTER_ALIGN
                    cell.font = Font(name="Arial", size=10)
                elif col == 4:
                    cell.font = Font(name="Arial", size=10, bold=True)
                    cell.alignment = CONTENT_ALIGN
                elif col in (6, 7):
                    cell.font = Font(name="Arial", size=10)
                    cell.alignment = CONTENT_ALIGN
                else:
                    cell.font = Font(name="Arial", size=9)
                    cell.alignment = CONTENT_ALIGN

            ws.row_dimensions[row].height = max(60, min(250, len(post.get("content", "")) * 0.4))

        await progress.emit(task_id, "step_progress", {
            "step": step_index, "progress": 0.5, "message": "帖子表已生成...",
        })

        # ===== Sheet 2: 统计信息 =====
        if include_stats:
            ws2 = wb.create_sheet("统计信息")

            # 基本统计
            stats_headers = [("统计项", 30), ("数值", 20)]
            for col, (header, width) in enumerate(stats_headers, 1):
                cell = ws2.cell(row=1, column=col, value=header)
                cell.font = HEADER_FONT
                cell.fill = HEADER_FILL
                cell.alignment = HEADER_ALIGN
                cell.border = THIN_BORDER
                ws2.column_dimensions[get_column_letter(col)].width = width

            usernames = [p.get("username", "") for p in posts]
            user_counter = Counter(usernames)
            unique_users = len(user_counter)
            pages = set(p.get("page_number", 1) for p in posts)
            timestamps = [p.get("timestamp", "") for p in posts if p.get("timestamp")]
            dated = [(_parse_dutch_timestamp(t), t) for t in timestamps]
            dated = [d for d in dated if d[0] is not None]

            stat_rows = [
                ("总帖子数", len(posts)),
                ("唯一用户数", unique_users),
                ("总页数", len(pages)),
                ("时间范围开始", min(dated)[1] if dated else "N/A"),
                ("时间范围结束", max(dated)[1] if dated else "N/A"),
            ]
            for i, (label, value) in enumerate(stat_rows):
                ws2.cell(row=i + 2, column=1, value=label).border = THIN_BORDER
                ws2.cell(row=i + 2, column=2, value=str(value)).border = THIN_BORDER
                ws2.cell(row=i + 2, column=2).alignment = CENTER_ALIGN

            # 活跃用户
            start_row = len(stat_rows) + 4
            ws2.cell(row=start_row, column=1, value="活跃用户排名").font = Font(
                name="Arial", size=12, bold=True
            )

            user_headers = [("用户", 20), ("帖子数", 10)]
            for col, (header, width) in enumerate(user_headers, 1):
                cell = ws2.cell(row=start_row + 1, column=col, value=header)
                cell.font = HEADER_FONT
                cell.fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
                cell.alignment = HEADER_ALIGN
                cell.border = THIN_BORDER

            for i, (user, count) in enumerate(user_counter.most_common(15)):
                row = start_row + 2 + i
                ws2.cell(row=row, column=1, value=user).border = THIN_BORDER
                ws2.cell(row=row, column=2, value=count).border = THIN_BORDER
                ws2.cell(row=row, column=2).alignment = CENTER_ALIGN

            ws2.freeze_panes = "A2"

        # 保存
        # 带时间戳，避免同一任务重跑时把上一次的导出直接盖掉
        output_name = f"tweakers_report_{task_id[:8]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        output_path = os.path.join(settings.exports_dir, output_name)
        _save_workbook(wb, output_path)

        await progress.emit(task_id, "step_progress", {
            "step": step_index,
            "progress": 1.0,
            "message": f"Excel 已保存: {output_name}",
        })

        return {
            "file_path": output_path,
            "file_name": output_name,
        }

    @staticmethod
    def generate_sentiment_report(
        task_id: str,
        sentiment_data: dict,
        output_dir: Optional[str] = None,
    ) -> dict:
        """生成舆情分析 Excel 报告"""
        if output_dir is None:
            output_dir = settings.exports_dir

        summary = sentiment_data.get("summary", {})
        results = sentiment_data.get("results", [])
        total = sentiment_data.get("total", 0)

        wb = Workbook()

        # ===== Sheet 1: 舆情分析汇总 =====
        ws = wb.active
        ws.title = "舆情分析汇总"

        # 样式
        SECTION_FONT = Font(name="Arial", size=13, bold=True, color="2F5496")
        LABEL_FONT = Font(name="Arial", size=11, bold=True)
        VALUE_FONT = Font(name="Arial", size=11)

        row = 1
        # 标题
        cell = ws.cell(row=row, column=1, value="HYXi Halo 舆情分析报告")
        cell.font = Font(name="Arial", size=16, bold=True, color="1E293B")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 2

        # 基本信息
        ws.cell(row=row, column=1, value="基本信息").font = SECTION_FONT
        row += 1
        info_rows = [
            ("分析帖子总数", total),
            ("分析成功数", sentiment_data.get("success", 0)),
            ("分析失败数", sentiment_data.get("failed", 0)),
            ("分析时间", sentiment_data.get("analyzed_at", "")),
        ]
        for label, value in info_rows:
            ws.cell(row=row, column=1, value=label).font = LABEL_FONT
            ws.cell(row=row, column=2, value=str(value)).font = VALUE_FONT
            row += 1

        row += 1
        # 情感分布
        ws.cell(row=row, column=1, value="情感分布").font = SECTION_FONT
        row += 1
        dist = summary.get("sentiment_distribution", {})
        pcts = summary.get("sentiment_percentages", {})

        headers = ["情感", "数量", "占比", "平均强度"]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=row, column=col, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGN
            cell.border = THIN_BORDER
        row += 1

        sentiment_labels = {"positive": "正面", "negative": "负面", "neutral": "中立"}
        sentiment_colors = {"positive": "10B981", "negative": "EF4444", "neutral": "6B7280"}
        for s_key, s_label in sentiment_labels.items():
            count = dist.get(s_key, 0)
            pct = pcts.get(s_key, 0)
            ws.cell(row=row, column=1, value=s_label).border = THIN_BORDER
            ws.cell(row=row, column=2, value=count).border = THIN_BORDER
            ws.cell(row=row, column=3, value=f"{pct}%").border = THIN_BORDER
            avg_intensity_label = summary.get("avg_intensity", 0)
            ws.cell(row=row, column=4, value=round(avg_intensity_label, 2) if isinstance(avg_intensity_label, (int, float)) else "").border = THIN_BORDER
            for c in range(1, 5):
                ws.cell(row=row, column=c).alignment = CENTER_ALIGN
            row += 1

        row += 1
        # Top 10 维度
        ws.cell(row=row, column=1, value="TOP 10 关注维度").font = SECTION_FONT
        row += 1
        dim_headers = ["排名", "维度", "提及次数"]
        for col, h in enumerate(dim_headers, 1):
            cell = ws.cell(row=row, column=col, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGN
            cell.border = THIN_BORDER
        row += 1

        top_dims = summary.get("top_dimensions", [])
        for rank, (dim, count) in enumerate(top_dims[:10], 1):
            ws.cell(row=row, column=1, value=rank).border = THIN_BORDER
            ws.cell(row=row, column=2, value=dim).border = THIN_BORDER
            ws.cell(row=row, column=3, value=count).border = THIN_BORDER
            for c in (1, 3):
                ws.cell(row=row, column=c).alignment = CENTER_ALIGN
            row += 1

        # 设置列宽
        ws.column_dimensions['A'].width = 18
        ws.column_dimensions['B'].width = 30
        ws.column_dimensions['C'].width = 14
        ws.column_dimensions['D'].width = 14
        ws.freeze_panes = "A2"

        # ===== Sheet 2: 帖子情感详情 =====
        ws2 = wb.create_sheet("帖子情感详情")

        detail_headers = ["序号", "情感", "强度", "分析理由", "涉及维度"]
        col_widths = [8, 10, 8, 50, 40]
        for col, (h, w) in enumerate(zip(detail_headers, col_widths), 1):
            cell = ws2.cell(row=1, column=col, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGN
            cell.border = THIN_BORDER
            ws2.column_dimensions[get_column_letter(col)].width = w

        ws2.freeze_panes = "A2"

        for idx, r in enumerate(results):
            row = idx + 2
            if not r:
                ws2.cell(row=row, column=1, value=idx + 1).border = THIN_BORDER
                ws2.cell(row=row, column=2, value="(解析失败)").border = THIN_BORDER
                for c in range(1, 6):
                    ws2.cell(row=row, column=c).alignment = CONTENT_ALIGN
                continue

            sentiment_cn = sentiment_labels.get(r.get("sentiment"), r.get("sentiment") or "(未分析)")
            intensity = r.get("intensity", 0)
            reason = r.get("reason_cn", "")
            dimensions = ", ".join(r.get("dimensions", []))

            stars = _star_level(intensity)
            values = [idx + 1, sentiment_cn, f"{'★' * stars}{'☆' * (5 - stars)} ({intensity})", reason, dimensions]
            for col, val in enumerate(values, 1):
                cell = ws2.cell(row=row, column=col, value=val)
                cell.border = THIN_BORDER
                cell.font = Font(name="Arial", size=10)
                if col in (1, 2, 3):
                    cell.alignment = CENTER_ALIGN
                else:
                    cell.alignment = CONTENT_ALIGN

            # 交替行颜色
            if idx % 2 == 1:
                for c in range(1, 6):
                    ws2.cell(row=row, column=c).fill = ALT_FILL

        # 保存
        output_name = f"sentiment_report_{task_id[:8]}.xlsx"
        output_path = os.path.join(output_dir, output_name)
        os.makedirs(output_dir, exist_ok=True)
        _save_workbook(wb, output_path)

        return {
            "file_path": output_path,
            "file_name": output_name,
        }
