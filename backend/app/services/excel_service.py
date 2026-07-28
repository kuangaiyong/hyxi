"""Excel 生成服务 - 复用 openpyxl 样式"""

import os
from collections import Counter
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from app.config import settings
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


class ExcelService:
    """生成翻译 Excel 报告"""

    @staticmethod
    async def execute(
        task_id: str,
        posts: list,
        params: dict,
        progress: ProgressManager,
    ) -> dict:
        """生成 Excel"""
        include_stats = params.get("include_stats", True)

        await progress.emit(task_id, "step_progress", {
            "step": 2,
            "progress": 0.0,
            "message": "正在生成 Excel 报告...",
        })

        wb = Workbook()

        # ===== Sheet 1: 论坛帖子翻译 =====
        ws = wb.active
        ws.title = "论坛帖子翻译"

        headers = [
            ("序号", 6), ("用户名", 16), ("发布时间", 18),
            ("原文（荷兰语）", 55), ("中文翻译", 55), ("页码", 6),
        ]
        for col, (header, width) in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGN
            cell.border = THIN_BORDER
            ws.column_dimensions[chr(64 + col)].width = width

        ws.freeze_panes = "A2"

        for idx, post in enumerate(posts):
            row = idx + 2
            fill = ALT_FILL if idx % 2 == 1 else PatternFill()

            values = [
                idx + 1,
                post.get("username", ""),
                post.get("timestamp", ""),
                post.get("content", ""),
                post.get("translation", ""),
                post.get("page_number", ""),
            ]
            for col, value in enumerate(values, 1):
                cell = ws.cell(row=row, column=col, value=value)
                cell.border = THIN_BORDER
                cell.fill = fill
                if col in (1, 6):
                    cell.alignment = CENTER_ALIGN
                    cell.font = Font(name="Arial", size=10)
                elif col == 2:
                    cell.font = Font(name="Arial", size=10, bold=True)
                    cell.alignment = CONTENT_ALIGN
                elif col in (4, 5):
                    cell.font = Font(name="Arial", size=10)
                    cell.alignment = CONTENT_ALIGN
                else:
                    cell.font = Font(name="Arial", size=9)
                    cell.alignment = CONTENT_ALIGN

            ws.row_dimensions[row].height = max(60, min(250, len(post.get("content", "")) * 0.4))

        await progress.emit(task_id, "step_progress", {
            "step": 2, "progress": 0.5, "message": "帖子表已生成...",
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
                ws2.column_dimensions[chr(64 + col)].width = width

            usernames = [p.get("username", "") for p in posts]
            user_counter = Counter(usernames)
            unique_users = len(user_counter)
            pages = set(p.get("page_number", 1) for p in posts)
            timestamps = [p.get("timestamp", "") for p in posts if p.get("timestamp")]

            stat_rows = [
                ("总帖子数", len(posts)),
                ("唯一用户数", unique_users),
                ("总页数", len(pages)),
                ("时间范围开始", timestamps[0] if timestamps else "N/A"),
                ("时间范围结束", timestamps[-1] if timestamps else "N/A"),
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
        output_name = f"tweakers_report_{task_id[:8]}.xlsx"
        output_path = os.path.join(settings.exports_dir, output_name)
        wb.save(output_path)

        await progress.emit(task_id, "step_progress", {
            "step": 2,
            "progress": 1.0,
            "message": f"Excel 已保存: {output_name}",
        })

        return {
            "file_path": output_path,
            "file_name": output_name,
        }
